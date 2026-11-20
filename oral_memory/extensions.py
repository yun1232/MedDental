"""Versioned utility feedback, delivery receipts, and non-destructive memory frequency."""

import json
import random
import uuid
from datetime import datetime, timezone

from schemas import FEEDBACK, FREQUENCY, MemoryError, validate


def create_tables(db):
    """Add schema-v2 tables without modifying existing memory or revision rows."""
    statements = [
        """CREATE TABLE IF NOT EXISTS memory_stats (
            patient_id TEXT NOT NULL, category TEXT NOT NULL, record_id TEXT NOT NULL, version INTEGER NOT NULL,
            retrieval_count INTEGER NOT NULL DEFAULT 0, exposure_count INTEGER NOT NULL DEFAULT 0,
            use_count INTEGER NOT NULL DEFAULT 0, positive REAL NOT NULL DEFAULT 0, negative REAL NOT NULL DEFAULT 0,
            last_used_at TEXT, frequency_override TEXT, protected INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(patient_id,category,record_id,version))""",
        """CREATE TABLE IF NOT EXISTS delivery_packets (
            patient_id TEXT NOT NULL, retrieval_id TEXT NOT NULL, refs TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(patient_id,retrieval_id))""",
        """CREATE TABLE IF NOT EXISTS memory_feedback (
            patient_id TEXT NOT NULL, event_id TEXT NOT NULL, retrieval_id TEXT NOT NULL,
            category TEXT NOT NULL, record_id TEXT NOT NULL, version INTEGER NOT NULL,
            kind TEXT NOT NULL, reason TEXT NOT NULL, actor_role TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(patient_id,event_id), UNIQUE(patient_id,retrieval_id,category,record_id,version,kind))""",
        """CREATE TABLE IF NOT EXISTS inquiry_cases (
            patient_id TEXT NOT NULL, encounter_id TEXT NOT NULL, revision INTEGER NOT NULL,
            state TEXT NOT NULL, PRIMARY KEY(patient_id,encounter_id))""",
        """CREATE TABLE IF NOT EXISTS workflow_events (
            patient_id TEXT NOT NULL, event_id TEXT NOT NULL, request_hash TEXT NOT NULL,
            response TEXT NOT NULL, request_json TEXT NOT NULL, PRIMARY KEY(patient_id,event_id))""",
        """CREATE TABLE IF NOT EXISTS frequency_events (
            patient_id TEXT NOT NULL,event_id TEXT NOT NULL,request_json TEXT NOT NULL,actor_role TEXT NOT NULL,
            created_at TEXT NOT NULL,PRIMARY KEY(patient_id,event_id))""",
        """CREATE TABLE IF NOT EXISTS doctor_tasks (
            patient_id TEXT NOT NULL, record_id TEXT NOT NULL, version INTEGER NOT NULL,
            encounter_id TEXT NOT NULL, target_field TEXT NOT NULL,
            PRIMARY KEY(patient_id,record_id,version))""",
        """CREATE TABLE IF NOT EXISTS doctor_answers (
            patient_id TEXT NOT NULL, encounter_id TEXT NOT NULL, record_id TEXT NOT NULL,
            version INTEGER NOT NULL, event_id TEXT NOT NULL, answer TEXT NOT NULL,
            sufficient INTEGER NOT NULL, source_ref TEXT NOT NULL,
            PRIMARY KEY(patient_id,encounter_id,record_id,version,event_id))""",
    ]
    for statement in statements:
        db.execute(statement)


def reference(record):
    """Return the category-qualified, version-specific identity of a memory."""
    return {key: record[key] for key in ("category", "record_id", "version")}


class UtilityMixin:
    """Calculate utility from auditable feedback; data is never removed for low use."""

    def memory_stats(self, record):
        row = self.db.execute("""SELECT * FROM memory_stats WHERE patient_id=? AND category=? AND record_id=? AND version=?""",
                              (self.patient_id, record["category"], record["record_id"], record["version"])).fetchone()
        values = dict(row) if row else {"retrieval_count": 0, "exposure_count": 0, "use_count": 0,
            "positive": 0.0, "negative": 0.0, "last_used_at": None, "protected": 0, "frequency_override": None}
        utility = (1 + values["positive"]) / (2 + values["positive"] + values["negative"])
        protected = bool(values["protected"]) or (record["category"] == "doctor" and (
            record.get("question_status") == "open" or record.get("note_type") == "final_diagnosis"))
        automatic = values["exposure_count"] >= self.settings["low_frequency_min_exposures"] and utility < self.settings["low_frequency_threshold"]
        frequency = values["frequency_override"] or ("low_frequency" if automatic else "normal")
        if protected:
            frequency = "normal"
        return {key: values[key] for key in ("retrieval_count", "exposure_count", "use_count", "positive", "negative", "last_used_at")} | {
            "utility_score": utility, "frequency": frequency, "protected": protected}

    def _ensure_stats(self, ref):
        self.db.execute("INSERT OR IGNORE INTO memory_stats(patient_id,category,record_id,version) VALUES (?,?,?,?)",
                        (self.patient_id, ref["category"], ref["record_id"], ref["version"]))

    def _deliver(self, result):
        refs = {}
        def collect(value):
            if isinstance(value, dict):
                if all(key in value for key in ("category", "record_id", "version", "search_text")):
                    ref = reference(value)
                    refs[(ref["category"], ref["record_id"], ref["version"])] = ref
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)
        collect(result)
        packet_id = str(uuid.uuid4())
        with self.transaction():
            self.db.execute("INSERT INTO delivery_packets VALUES (?,?,?,?)", (self.patient_id, packet_id,
                json.dumps(list(refs.values())), datetime.now(timezone.utc).isoformat()))
            for ref in refs.values():
                self._ensure_stats(ref)
                self.db.execute("""UPDATE memory_stats SET retrieval_count=retrieval_count+1, exposure_count=exposure_count+1
                    WHERE patient_id=? AND category=? AND record_id=? AND version=?""",
                    (self.patient_id, ref["category"], ref["record_id"], ref["version"]))
        return {**result, "retrieval_id": packet_id, "delivered_refs": list(refs.values())}

    def memory_feedback(self, arguments):
        """Accept feedback only for delivered versions, with duplicate-event protection."""
        validate(arguments, FEEDBACK)
        if self.role != "doctor" and arguments["kind"] != "used":
            raise MemoryError("forbidden", "Only doctor callers may evaluate usefulness or unused opportunities")
        ref = arguments["reference"]
        with self.transaction():
            row = self.db.execute("SELECT refs FROM delivery_packets WHERE patient_id=? AND retrieval_id=?",
                                  (self.patient_id, arguments["retrieval_id"])).fetchone()
            if row is None or ref not in json.loads(row["refs"]):
                raise MemoryError("not_exposed", "Reference was not delivered in this patient's retrieval packet")
            existing = self.db.execute("SELECT * FROM memory_feedback WHERE patient_id=? AND event_id=?",
                                       (self.patient_id, arguments["event_id"])).fetchone()
            if existing:
                expected = (arguments["retrieval_id"], ref["category"], ref["record_id"], ref["version"], arguments["kind"], arguments["reason"])
                actual = tuple(existing[key] for key in ("retrieval_id", "category", "record_id", "version", "kind", "reason"))
                if expected != actual:
                    raise MemoryError("event_conflict", "Feedback event ID was reused with different data")
                return {"idempotent": True}
            duplicate = self.db.execute("""SELECT 1 FROM memory_feedback WHERE patient_id=? AND retrieval_id=?
                AND category=? AND record_id=? AND version=? AND kind=?""", (self.patient_id, arguments["retrieval_id"],
                ref["category"], ref["record_id"], ref["version"], arguments["kind"])).fetchone()
            if duplicate:
                return {"idempotent": True}
            if arguments["kind"] != "used":
                verdict = self.db.execute("""SELECT kind FROM memory_feedback WHERE patient_id=? AND retrieval_id=?
                    AND category=? AND record_id=? AND version=? AND kind!='used'""", (self.patient_id,
                    arguments["retrieval_id"], ref["category"], ref["record_id"], ref["version"])).fetchone()
                if verdict:
                    raise MemoryError("feedback_conflict", "This delivery already has a usefulness verdict")
            stamp = datetime.now(timezone.utc).isoformat()
            self.db.execute("INSERT INTO memory_feedback VALUES (?,?,?,?,?,?,?,?,?,?)", (self.patient_id,
                arguments["event_id"], arguments["retrieval_id"], ref["category"], ref["record_id"], ref["version"],
                arguments["kind"], arguments["reason"], self.role, stamp))
            positive = 1.0 if arguments["kind"] == "helpful" else 0.0
            negative = {"not_helpful": 1.0, "unused_opportunity": self.settings["unused_penalty"]}.get(arguments["kind"], 0.0)
            used = int(arguments["kind"] == "used")
            self.db.execute("""UPDATE memory_stats SET positive=positive+?,negative=negative+?,use_count=use_count+?,
                last_used_at=CASE WHEN ?=1 THEN ? ELSE last_used_at END
                WHERE patient_id=? AND category=? AND record_id=? AND version=?""",
                (positive, negative, used, used, stamp, self.patient_id, ref["category"], ref["record_id"], ref["version"]))
        return {"idempotent": False, "feedback": arguments["kind"]}

    def set_frequency(self, arguments):
        """Set a recoverable recall frequency and protection; never delete records."""
        validate(arguments, FREQUENCY)
        if self.role != "doctor":
            raise MemoryError("forbidden", "Frequency overrides require a doctor-bound caller")
        ref = arguments["reference"]
        current = self.get({"category": ref["category"], "record_id": ref["record_id"], "include_versions": False})["record"]
        if current["version"] != ref["version"]:
            raise MemoryError("version_conflict", "Frequency changes must reference the current memory version")
        with self.transaction():
            self._ensure_stats(ref)
            self.db.execute("""UPDATE memory_stats SET frequency_override=?,protected=?
                WHERE patient_id=? AND category=? AND record_id=? AND version=?""", (arguments["frequency"],
                int(arguments["protected"]), self.patient_id, ref["category"], ref["record_id"], ref["version"]))
            self.db.execute("INSERT INTO frequency_events VALUES (?,?,?,?,?)", (self.patient_id, str(uuid.uuid4()),
                json.dumps(arguments, ensure_ascii=False), self.role, datetime.now(timezone.utc).isoformat()))
        return {"reference": ref, "stats": self.memory_stats(current), "reason": arguments["reason"]}


def select_ties(items, limit, keys, record_key, settings, rng=None):
    """Sample only within exactly equal relevance groups, preserving primary priorities."""
    rng = rng or random.SystemRandom()
    ordered = sorted(items, key=lambda item: tuple(-item[key] for key in keys))
    result = []
    index = 0
    while index < len(ordered) and len(result) < limit:
        group = [ordered[index]]
        index += 1
        while index < len(ordered) and all(ordered[index][key] == group[0][key] for key in keys):
            group.append(ordered[index])
            index += 1
        while group and len(result) < limit:
            protected = [item for item in group if item[record_key].get("stats", {}).get("protected")]
            if protected:
                chosen = sorted(protected, key=lambda item: item[record_key]["record_id"])[0]
            else:
                weights = []
                for item in group:
                    stats = item[record_key].get("stats", {})
                    weight = stats.get("utility_score", 0.5) + settings["utility_floor"]
                    if stats.get("frequency") == "low_frequency":
                        weight *= settings["low_frequency_multiplier"]
                    weights.append(weight)
                total = sum(weights)
                probabilities = [(1-settings["exploration_rate"])*weight/total + settings["exploration_rate"]/len(group) for weight in weights]
                chosen = rng.choices(group, weights=probabilities, k=1)[0]
            result.append(chosen)
            group.remove(chosen)
    return result
