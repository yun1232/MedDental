"""Published-case adapter over the original four-category retrieval and utility contracts.

Published snapshots and original counters are preserved. Only the original
memory_stats/feedback mechanism remains authoritative after the one-time upgrade.
"""
import hashlib
import json
import sqlite3
from pathlib import Path
from extensions import create_tables
from schemas import MemoryError, GET, PHENOTYPE, validate
from store import MemoryStore, CATEGORIES

APPLICATION_ID = 0x4D444331


def library_identity(domain):
    return "case-library:" + hashlib.sha256(domain.encode()).hexdigest()


def record_id(case_id, domain, category):
    key = json.dumps([domain, case_id], ensure_ascii=False)
    return "library-" + hashlib.sha256(key.encode()).hexdigest() + "-" + category


class CaseLibrary(MemoryStore):
    def __init__(self, path, domain, role, settings):
        self.domain, self.role, self.settings = domain, role, settings
        self.patient_id = library_identity(domain)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, timeout=settings["busy_timeout_ms"] / 1000)
        self.db.row_factory = sqlite3.Row
        try:
            application = self.db.execute("PRAGMA application_id").fetchone()[0]
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            tables = self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
            if (application, version) not in ((0, 0), (APPLICATION_ID, 1), (APPLICATION_ID, 2)) or (application == 0 and tables):
                raise MemoryError("wrong_database", "Shared case library must be a new file or a MedDental case library")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            with self.transaction():
                for statement in (
                    '''CREATE TABLE IF NOT EXISTS cases (
                      id TEXT PRIMARY KEY, version INTEGER NOT NULL, domain TEXT NOT NULL,
                      payload TEXT NOT NULL, helpful INTEGER NOT NULL DEFAULT 0,
                      unhelpful INTEGER NOT NULL DEFAULT 0, exposures INTEGER NOT NULL DEFAULT 0)''',
                    '''CREATE TABLE IF NOT EXISTS case_versions (
                      id TEXT NOT NULL, version INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(id,version))''',
                    '''CREATE TABLE IF NOT EXISTS deliveries (
                      run TEXT NOT NULL, id TEXT NOT NULL, version INTEGER NOT NULL,
                      feedback TEXT, PRIMARY KEY(run,id,version))''',
                    '''CREATE TABLE IF NOT EXISTS case_memory_refs (
                      case_id TEXT NOT NULL, version INTEGER NOT NULL, domain TEXT NOT NULL,
                      category TEXT NOT NULL, record_id TEXT NOT NULL,
                      PRIMARY KEY(case_id,version,category))''',
                    '''CREATE INDEX IF NOT EXISTS case_memory_identity ON case_memory_refs(domain,category,record_id,version)''',
                ):
                    self.db.execute(statement)
                create_tables(self.db)
                if version < 2:
                    for row in self.db.execute("SELECT payload FROM case_versions ORDER BY id,version").fetchall():
                        self.index_case(json.loads(row["payload"]))
                    for row in self.db.execute("SELECT * FROM cases").fetchall():
                        case = json.loads(row["payload"])
                        self.index_case(case)
                        owner = library_identity(case["domain"])
                        rid = record_id(case["case_id"], case["domain"], "lesion")
                        self.db.execute('''INSERT OR IGNORE INTO memory_stats
                            (patient_id,category,record_id,version,exposure_count,retrieval_count,positive,negative,decay_score)
                            VALUES(?,?,?,?,?,?,?,?,?)''', (owner, "lesion", rid, case["version"],
                            row["exposures"], row["exposures"], row["helpful"], row["unhelpful"], max(0, round(1-.1*row["unhelpful"],10))))
                    # Older versions have no current header counters: reconstruct their known history from receipts.
                    receipts = self.db.execute("SELECT d.*,v.payload FROM deliveries d JOIN case_versions v ON v.id=d.id AND v.version=d.version").fetchall()
                    history = {}
                    for receipt in receipts:
                        group = history.setdefault((receipt["id"], receipt["version"]), {"version": receipt["version"], "payload": receipt["payload"], "exposures": 0, "positive": 0, "negative": 0})
                        group["exposures"] += 1
                        verdict = json.loads(receipt["feedback"])["kind"] if receipt["feedback"] else None
                        group["positive"] += int(verdict == "helpful")
                        group["negative"] += int(verdict == "not_helpful")
                    for old in history.values():
                        snapshot = json.loads(old["payload"])
                        self.db.execute("""INSERT OR IGNORE INTO memory_stats
                            (patient_id,category,record_id,version,exposure_count,retrieval_count,positive,negative,decay_score)
                            VALUES(?,?,?,?,?,?,?,?,?)""", (library_identity(snapshot["domain"]), "lesion",
                            record_id(snapshot["case_id"], snapshot["domain"], "lesion"), old["version"], old["exposures"], old["exposures"],
                            old["positive"], old["negative"], max(0, round(1-.1*old["negative"],10))))
                    # Recover receipts too, allowing old consultations to submit feedback through the common API.
                    for row in receipts:
                        case = json.loads(row["payload"])
                        ref = {"category": "lesion", "record_id": record_id(case["case_id"], case["domain"], "lesion"), "version": row["version"]}
                        receipt = self.legacy_receipt(row["run"], row["id"], row["version"])
                        owner = library_identity(case["domain"])
                        self.db.execute("INSERT OR IGNORE INTO delivery_packets(patient_id,retrieval_id,refs,created_at,consultation_id) VALUES(?,?,?,?,?)", (owner, receipt, json.dumps([ref]), "legacy-import", row["run"]))
                        if row["feedback"]:
                            feedback = json.loads(row["feedback"])
                            self.db.execute("INSERT OR IGNORE INTO memory_feedback VALUES(?,?,?,?,?,?,?,?,?,?)", (owner, "legacy:" + receipt, receipt,
                                "lesion", ref["record_id"], ref["version"], feedback["kind"], feedback["reason"], "doctor", "legacy-import"))
                self.db.execute(f"PRAGMA application_id={APPLICATION_ID}")
                self.db.execute("PRAGMA user_version=2")
            path.chmod(0o600)
        except BaseException:
            self.db.close()
            raise

    @staticmethod
    def legacy_receipt(run_id, case_id, version):
        return "legacy:" + hashlib.sha256(json.dumps([run_id, case_id, version]).encode()).hexdigest()

    def index_case(self, case):
        for category in CATEGORIES:
            self.db.execute("INSERT OR IGNORE INTO case_memory_refs VALUES(?,?,?,?,?)", (case["case_id"], case["version"], case["domain"], category,
                            record_id(case["case_id"], case["domain"], category)))

    def _project(self, case, category, order=0):
        rid = record_id(case["case_id"], case["domain"], category)
        lesion_id = record_id(case["case_id"], case["domain"], "lesion")
        common = {"record_id": rid, "encounter_id": "published:" + lesion_id, "event_time": "unknown",
                  "source_ref": case["source_ref"], "source": "doctor", "review_status": "doctor_confirmed"}
        if case.get('supervision') == 'offline_labels' and category in ('lesion', 'experience'):
            # Dataset labels supervise the targets; they do not confirm model observations.
            common.update(source='model' if category == 'lesion' else 'patient', review_status='unreviewed')
        if category == "lesion":
            value = {**common, "lesion_id": lesion_id,
                     "features": case.get("features") or {key: [] if key == "tooth_positions" else "unknown" for key in PHENOTYPE},
                     "visual_description": " ".join(case["phenotype"]), "size_description": "unknown",
                     "pathology": {"status": "unknown", "result": "", "source_ref": ""}}
            search_text = value["visual_description"]
        elif category == "experience":
            value = {**common, "lesion_ids": [lesion_id], "experience_type": "other_oral", "description": " ".join(case["context"])}
            search_text = value["description"]
        elif category == "care":
            value = {**common, "lesion_ids": [lesion_id], "care_type": "diagnosis",
                     "description": case["experience"], "outcome": case["outcome"]}
            search_text = value["description"] + " " + value["outcome"]
        else:
            value = {**common, "lesion_ids": [lesion_id], "note_type": "supplement", "description": " ".join([*case["evidence"], *case["pitfalls"]]),
                     "related_record_ids": [], "question_status": "not_applicable", "answer": ""}
            search_text = value["description"]
        record = {**value, "patient_id": library_identity(case["domain"]), "category": category, "version": case["version"],
                  "updated_order": order, "updated_at": "unknown", "search_text": search_text,
                  "case_id": case["case_id"], "memory_scope": "library"}
        record["stats"] = self.memory_stats(record)
        record["visual"] = [{"reference": {"category": "lesion", "record_id": lesion_id, "version": case["version"]},
            "image": image, "regions": [r for r in case["regions"] if r["image_id"] == image["image_id"]],
            "findings": [], "source_run": "published-case", "annotation_version": case["version"], "stale": False} for image in case["images"]] if category == "lesion" else []
        return record

    def put(self, category, arguments):
        raise MemoryError("read_only_projection", "Use the authorized case-publication path to change published snapshots")

    def records(self, category):
        if category == "patient_experience":
            from patient_experience import normalize
            return [normalize(r) for origin in ("experience", "care") for r in self.records(origin)]
        if category not in CATEGORIES:
            raise MemoryError("invalid_input", "Unknown memory category")
        rows = self.db.execute("SELECT rowid,* FROM cases WHERE domain=? ORDER BY rowid LIMIT ?", (self.domain, self.settings["max_scan_records"] + 1)).fetchall()
        if len(rows) > self.settings["max_scan_records"]:
            raise MemoryError("scan_limit", "Shared case library exceeds the original memory scan budget")
        return [self._project(json.loads(row["payload"]), category, row["rowid"]) for row in rows]

    def get(self, arguments):
        validate(arguments, GET)
        row = self.db.execute('''SELECT c.rowid,c.payload FROM case_memory_refs r JOIN cases c ON c.id=r.case_id AND c.version=r.version
            WHERE r.domain=? AND r.category=? AND r.record_id=?''', (self.domain, arguments["category"], arguments["record_id"])).fetchone()
        if row is None:
            raise MemoryError("not_found", "Published memory not found in the configured domain")
        value = {"record": self._project(json.loads(row["payload"]), arguments["category"], row["rowid"])}
        if arguments["include_versions"]:
            versions = self.db.execute('''SELECT v.version,v.payload FROM case_memory_refs r JOIN case_versions v ON v.id=r.case_id AND v.version=r.version
                WHERE r.domain=? AND r.category=? AND r.record_id=? ORDER BY r.version''', (self.domain, arguments["category"], arguments["record_id"])).fetchall()
            value["versions"] = [{"version": v["version"], "record": self._project(json.loads(v["payload"]), arguments["category"]), "actor_role": "doctor", "changed_at": "unknown"} for v in versions]
        return value

    def published(self, case_id, version):
        row = self.db.execute("SELECT payload FROM case_versions WHERE id=? AND version=?", (case_id, version)).fetchone()
        if row is None or json.loads(row["payload"])["domain"] != self.domain:
            raise MemoryError("not_found", "Published case version not found")
        return json.loads(row["payload"])
