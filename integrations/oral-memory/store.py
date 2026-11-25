"""Three business memory groups with version-stable legacy storage, bound to one patient."""

import json
import math
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from retrieval import hierarchical, staged, rank_text, render_lesion
from schemas import GET, PHENOTYPE, RECORDS, RETRIEVE, SEARCH, WRITE_SCHEMAS, MemoryError, validate
from extensions import UtilityMixin, create_tables
from visual_memory import VisualMemoryMixin

APPLICATION_ID = 0x4F4C4B31
SCHEMA_VERSION = 2
# Backing categories preserve old record/feedback identities; new business has one experience category.
CATEGORIES = tuple(RECORDS)
BUSINESS_CATEGORIES = ("lesion", "patient_experience", "doctor")


def settings_from_env(env):
    """Resolve and validate deployment tuning before opening a memory database."""
    try:
        weights = json.loads(env.get("ORAL_MEMORY_FIELD_WEIGHTS", "{}"))
        if not isinstance(weights, dict) or weights.keys() - (PHENOTYPE.keys() | {"text"}):
            raise ValueError("invalid field weights")
        resolved = {key: float(weights.get(key, 1.0)) for key in (*PHENOTYPE, "text")}
        if any(not math.isfinite(weight) or weight <= 0 for weight in resolved.values()):
            raise ValueError("weights must be finite and positive")
        result = {
            "weights": resolved,
            "case_driven": env.get("ORAL_MEMORY_CASE_DRIVEN", "0") == "1",
            "min_phenotype_score": float(env.get("ORAL_MEMORY_MIN_PHENOTYPE_SCORE", "0.2")),
            "recall_limit": int(env.get("ORAL_MEMORY_RECALL_LIMIT", "100")),
            "per_category_limit": int(env.get("ORAL_MEMORY_PER_CATEGORY_LIMIT", "10")),
            "max_scan_records": int(env.get("ORAL_MEMORY_MAX_SCAN_RECORDS", "10000")),
            "busy_timeout_ms": int(env.get("ORAL_MEMORY_BUSY_TIMEOUT_MS", "5000")),
            "low_frequency_min_exposures": int(env.get("ORAL_MEMORY_LOW_FREQUENCY_MIN_EXPOSURES", "10")),
            "low_frequency_threshold": float(env.get("ORAL_MEMORY_LOW_FREQUENCY_THRESHOLD", "0.3")),
            "low_frequency_multiplier": float(env.get("ORAL_MEMORY_LOW_FREQUENCY_MULTIPLIER", "0.2")),
            "exploration_rate": float(env.get("ORAL_MEMORY_EXPLORATION_RATE", "0.1")),
            "utility_floor": float(env.get("ORAL_MEMORY_UTILITY_FLOOR", "0.05")),
            "unused_penalty": float(env.get("ORAL_MEMORY_UNUSED_PENALTY", "0.1")),
            "decay_step": float(env.get("ORAL_MEMORY_DECAY_STEP", "0.1")),
            "decay_floor": float(env.get("ORAL_MEMORY_DECAY_FLOOR", "0")),
            "max_question_attempts": int(env.get("ORAL_MEMORY_MAX_QUESTION_ATTEMPTS", "2")),
        }
        if not 0 < result["min_phenotype_score"] <= 1:
            raise ValueError("phenotype threshold must be in (0,1]")
        if not 1 <= result["per_category_limit"] <= 50 or not 1 <= result["recall_limit"] <= 10000:
            raise ValueError("retrieval limits out of bounds")
        if not 1 <= result["max_scan_records"] <= 1000000 or not 0 <= result["busy_timeout_ms"] <= 60000:
            raise ValueError("scan or timeout limit out of bounds")
        for field in ("low_frequency_threshold", "low_frequency_multiplier", "exploration_rate", "utility_floor", "unused_penalty"):
            if not math.isfinite(result[field]) or not 0 < result[field] <= 1:
                raise ValueError(f"{field} must be finite and in (0,1]")
        if not math.isfinite(result["decay_step"]) or not 0 < result["decay_step"] <= 1 or not math.isfinite(result["decay_floor"]) or not 0 <= result["decay_floor"] <= 1:
            raise ValueError("invalid decay step or floor")
        if not 1 <= result["low_frequency_min_exposures"] <= 100000 or not 1 <= result["max_question_attempts"] <= 10:
            raise ValueError("exposure or question-attempt limit out of bounds")
        return result
    except (ValueError, TypeError, OverflowError) as error:
        raise MemoryError("invalid_config", f"Invalid oral-memory configuration: {error}") from error


class MemoryStore(VisualMemoryMixin, UtilityMixin):
    """Own a database connection; never accept patient or actor identity from tools."""

    def __init__(self, path, patient_id, role="patient", settings=None):
        if not patient_id or not patient_id.strip():
            raise MemoryError("invalid_config", "ORAL_MEMORY_PATIENT_ID is required")
        if role not in ("patient", "doctor"):
            raise MemoryError("invalid_config", "ORAL_MEMORY_ACTOR_ROLE must be patient or doctor")
        self.patient_id = patient_id
        self.role = role
        self.settings = settings if settings is not None else settings_from_env({})
        path = Path(path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        self.db = sqlite3.connect(path, timeout=self.settings["busy_timeout_ms"] / 1000)
        self.db.row_factory = sqlite3.Row
        try:
            self._initialize()
        except BaseException:
            self.db.close()
            raise

    def _initialize(self):
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        application = self.db.execute("PRAGMA application_id").fetchone()[0]
        existing = self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if version not in (0, 1, SCHEMA_VERSION) or application not in (0, APPLICATION_ID):
            raise MemoryError("unsupported_database", "Database belongs to another application or schema version")
        if version == 0 and existing:
            raise MemoryError("unsupported_database", "Refusing to initialize an existing non-memory database")
        if version > 0 and application != APPLICATION_ID:
            raise MemoryError("unsupported_database", "Missing oral-memory application marker")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("""CREATE TABLE IF NOT EXISTS revisions (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, patient_id TEXT NOT NULL,
                category TEXT NOT NULL, record_id TEXT NOT NULL, version INTEGER NOT NULL,
                payload TEXT NOT NULL, actor_role TEXT NOT NULL, changed_at TEXT NOT NULL,
                UNIQUE(patient_id,category,record_id,version))""")
            for category in CATEGORIES:
                self.db.execute(f"""CREATE TABLE IF NOT EXISTS {category}_memory (
                    patient_id TEXT NOT NULL, record_id TEXT NOT NULL, encounter_id TEXT NOT NULL,
                    lesion_id TEXT, payload TEXT NOT NULL, search_text TEXT NOT NULL,
                    version INTEGER NOT NULL, updated_order INTEGER NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(patient_id,record_id))""")
                self.db.execute(f"CREATE INDEX IF NOT EXISTS {category}_by_encounter "
                                f"ON {category}_memory(patient_id,encounter_id)")
                self.db.execute(f"CREATE INDEX IF NOT EXISTS {category}_by_lesion "
                                f"ON {category}_memory(patient_id,lesion_id)")
            self.db.execute("""CREATE VIEW IF NOT EXISTS patient_experience_memory AS
                SELECT *, 'experience' AS origin_category FROM experience_memory
                UNION ALL SELECT *, 'care' AS origin_category FROM care_memory""")
            create_tables(self.db)
            self.db.execute(f"PRAGMA application_id={APPLICATION_ID}")
            self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self):
        """Close the owned connection; committed records survive server restarts."""
        self.db.close()

    @contextmanager
    def transaction(self):
        """Join an outer workflow transaction; only the owner commits or rolls it back."""
        if self.db.in_transaction:
            yield
            return
        try:
            self.db.execute("BEGIN IMMEDIATE")
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def _authorize(self, category, record):
        if self.role != "doctor" and (category == "doctor" or record["source"] == "doctor"
                                     or record["review_status"] == "doctor_confirmed"):
            raise MemoryError("forbidden", "Doctor records and confirmations require a doctor-bound host")
        if category == "doctor":
            if record["source"] != "doctor" or record["review_status"] != "doctor_confirmed":
                raise MemoryError("invalid_input", "Doctor notes must name doctor as their source and be confirmed")
            if record["note_type"] == "follow_up":
                if record["question_status"] not in ("open", "answered", "cancelled"):
                    raise MemoryError("invalid_input", "A follow-up question must be open or answered")
                if (record["question_status"] == "answered") != bool(record["answer"].strip()):
                    raise MemoryError("invalid_input", "Only answered follow-up questions carry an answer")
            elif record["question_status"] != "not_applicable" or record["answer"]:
                raise MemoryError("invalid_input", "Non-question notes cannot carry question status or answers")
        if category == "lesion":
            pathology = record["pathology"]
            if pathology["status"] == "unknown":
                if pathology["result"] or pathology["source_ref"]:
                    raise MemoryError("invalid_input", "Unknown pathology must not contain an inferred result")
            elif not pathology["result"].strip() or not pathology["source_ref"].strip():
                raise MemoryError("invalid_input", "Pathology results require a report or doctor source reference")
            elif pathology["status"] == "doctor_confirmed" and self.role != "doctor":
                raise MemoryError("forbidden", "Only a doctor-bound host can confirm pathology")

    def _decode(self, row, category):
        payload = json.loads(row["payload"])
        validate(payload, RECORDS[category], "stored_record")
        record = {**payload, "category": category, "patient_id": self.patient_id,
                "version": row["version"], "updated_at": row["updated_at"],
                "updated_order": row["updated_order"], "search_text": row["search_text"]}
        record["stats"] = self.memory_stats(record)
        record["visual"] = self.visual_context(record)
        if category == "lesion" and (record["visual"] or record.get("visual_description")):
            from retrieval import descriptor_text
            record["search_text"] += " VISUAL/1 " + descriptor_text(record)
        return record

    def put(self, category, arguments):
        """Atomically append a revision and replace current data, with optimistic locking."""
        if category == "patient_experience":
            from patient_experience import put
            return put(self, arguments)
        if category not in CATEGORIES:
            raise MemoryError("invalid_input", "Unknown memory category")
        validate(arguments, WRITE_SCHEMAS[category])
        record = arguments["record"]
        self._authorize(category, record)
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        search_text = render_lesion(record) if category == "lesion" else " ".join(
            str(record[key]) for key in ("description", "outcome", "answer", "question_status", "note_type") if key in record)
        changed_at = datetime.now(timezone.utc).isoformat()
        with self.transaction():
            previous = self.db.execute(f"SELECT * FROM {category}_memory WHERE patient_id=? AND record_id=?",
                                       (self.patient_id, record["record_id"])).fetchone()
            if previous and previous["payload"] == payload:
                return {"record": self._decode(previous, category), "idempotent": True}
            actual = previous["version"] if previous else 0
            if actual != arguments["expected_version"]:
                raise MemoryError("version_conflict", f"Expected version {arguments['expected_version']}; current version is {actual}")
            if previous:
                old = json.loads(previous["payload"])
                if old["encounter_id"] != record["encounter_id"] or (category == "lesion" and old["lesion_id"] != record["lesion_id"]):
                    raise MemoryError("invalid_input", "Record encounter and lesion identities are immutable; create a new record")
                if old["review_status"] == "doctor_confirmed" and self.role != "doctor":
                    raise MemoryError("forbidden", "A patient host cannot overwrite a doctor-confirmed record")
            version = actual + 1
            cursor = self.db.execute("""INSERT INTO revisions
                (patient_id,category,record_id,version,payload,actor_role,changed_at) VALUES (?,?,?,?,?,?,?)""",
                (self.patient_id, category, record["record_id"], version, payload, self.role, changed_at))
            order = cursor.lastrowid
            self.db.execute(f"""INSERT INTO {category}_memory
                (patient_id,record_id,encounter_id,lesion_id,payload,search_text,version,updated_order,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(patient_id,record_id) DO UPDATE SET
                payload=excluded.payload,search_text=excluded.search_text,version=excluded.version,
                updated_order=excluded.updated_order,updated_at=excluded.updated_at""",
                (self.patient_id, record["record_id"], record["encounter_id"], record.get("lesion_id"),
                 payload, search_text, version, order, changed_at))
        return {"record": {**record, "patient_id": self.patient_id, "category": category,
                           "version": version, "updated_order": order, "updated_at": changed_at,
                           "search_text": search_text}, "idempotent": False}

    def records(self, category):
        """Load one patient's category; refuse oversize scans rather than hide truncation."""
        if category == "patient_experience":
            from patient_experience import normalize
            rows = self.db.execute("SELECT * FROM patient_experience_memory WHERE patient_id=? LIMIT ?", (self.patient_id, self.settings["max_scan_records"] + 1)).fetchall()
            if len(rows) > self.settings["max_scan_records"]:
                raise MemoryError("scan_limit", "Patient experience exceeds the unified scan budget")
            return [normalize(self._decode(row, row["origin_category"])) for row in rows]
        rows = self.db.execute(f"SELECT * FROM {category}_memory WHERE patient_id=? LIMIT ?",
                               (self.patient_id, self.settings["max_scan_records"] + 1)).fetchall()
        if len(rows) > self.settings["max_scan_records"]:
            raise MemoryError("scan_limit", "Patient category exceeds ORAL_MEMORY_MAX_SCAN_RECORDS; add a larger-scale index")
        return [self._decode(row, category) for row in rows]

    def get(self, arguments):
        """Read a current record and, when requested, its immutable prior revisions."""
        validate(arguments, GET)
        category = arguments["category"]
        row = self.db.execute(f"SELECT * FROM {category}_memory WHERE patient_id=? AND record_id=?",
                              (self.patient_id, arguments["record_id"])).fetchone()
        if row is None:
            raise MemoryError("not_found", "Record not found for the host-bound patient")
        result = {"record": self._decode(row, category)}
        if arguments["include_versions"]:
            versions = self.db.execute("""SELECT version,payload,actor_role,changed_at FROM revisions
                WHERE patient_id=? AND category=? AND record_id=? ORDER BY version""",
                (self.patient_id, category, arguments["record_id"])).fetchall()
            result["versions"] = [{"version": item["version"], "record": json.loads(item["payload"]),
                                   "actor_role": item["actor_role"], "changed_at": item["changed_at"]}
                                  for item in versions]
        return result

    def get_patient_experience(self, arguments):
        from schemas import PATIENT_EXPERIENCE_GET
        from patient_experience import normalize
        validate(arguments, PATIENT_EXPERIENCE_GET)
        ref = arguments["reference"]
        if ref["category"] not in ("experience", "care"):
            raise MemoryError("invalid_category", "Patient experience reference must identify an oral/care event")
        value = self.get({"category": ref["category"], "record_id": ref["record_id"], "include_versions": arguments["include_versions"]})
        if value["record"]["version"] != ref["version"]:
            raise MemoryError("version_conflict", "Re-read the current patient experience version")
        if "versions" in value:
            value["versions"] = [{**item, "record": normalize({**item["record"], "category": ref["category"], "version": item["version"]})} for item in value["versions"]]
        return {**value, "record": normalize(value["record"])}

    def search(self, arguments):
        """Search exactly one category, independently of hierarchical case ranking."""
        validate(arguments, SEARCH)
        records = self.records(arguments["category"])
        for field in ("note_type", "question_status"):
            if field in arguments:
                if arguments["category"] != "doctor":
                    raise MemoryError("invalid_input", f"{field} applies only to doctor memory")
                records = [record for record in records if record[field] == arguments[field]]
        if "encounter_id" in arguments:
            records = [record for record in records if record["encounter_id"] == arguments["encounter_id"]]
        if "lesion_id" in arguments:
            records = [record for record in records if record.get("lesion_id") == arguments["lesion_id"]
                       or arguments["lesion_id"] in record.get("lesion_ids", [])]
        return self._deliver({"category": arguments["category"], "matches": rank_text(records, arguments["query"], arguments["top_k"], self.settings),
                "mode": "relevance" if arguments["query"].strip() else "recent_writes"})

    def retrieve(self, arguments, *, sources=(), initial_only=False, consultation_id=None):
        """Run an independently scored, phenotype-first two-level retrieval."""
        validate(arguments, RETRIEVE)
        with self.transaction():
            records = {category: self.records(category) for category in CATEGORIES}
            for source in sources:
                for category in CATEGORIES:
                    records[category].extend(source.records(category))
            settings = {**self.settings, "initial_only": initial_only}
            purpose = arguments.get("purpose", "legacy")
            result = staged(records, arguments, settings, purpose) if purpose != "legacy" else hierarchical(records, arguments, settings)
            if initial_only and purpose == "legacy":
                result["ranking"] = ["phenotype_score DESC", "care_score DESC", "stable initial ties; iteration score applied only after model scoring"]
            from workflow import PatientCommunicationAgent
            result["required_context"] = PatientCommunicationAgent(self).required_context(arguments.get("encounter_id"))
            delivered = self._deliver(result, consultation_id=consultation_id)
            delivered["source_packets"] = [{"scope": "patient", "retrieval_id": delivered["retrieval_id"], "delivered_refs": delivered["delivered_refs"]}]
            for source in sources:
                packet = source._deliver(result, consultation_id=consultation_id)
                delivered["source_packets"].append({"scope": "library", "retrieval_id": packet["retrieval_id"], "delivered_refs": packet["delivered_refs"]})
            return delivered
