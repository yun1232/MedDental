"""Image evidence attached to the existing category/record/version memory identity."""
import json
from datetime import datetime, timezone
from schemas import MemoryError, REFERENCE, PHENOTYPE, choice, obj, strings, text, validate

IMAGE = obj({"image_id": text(), "media_type": choice("image/png"),
             "width": {"type": "integer", "minimum": 1}, "height": {"type": "integer", "minimum": 1},
             "sha256": text(), "bytes": {"type": "integer", "minimum": 1}, "source_message_ids": strings()},
            ("image_id", "media_type", "width", "height", "sha256", "bytes"))


def create_visual_tables(db):
    for statement in (
        '''CREATE TABLE IF NOT EXISTS memory_visual_revisions (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, patient_id TEXT NOT NULL,
          category TEXT NOT NULL, record_id TEXT NOT NULL, record_version INTEGER NOT NULL,
          image_id TEXT NOT NULL, event_id TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(patient_id,event_id))''',
        '''CREATE INDEX IF NOT EXISTS memory_visual_lookup ON memory_visual_revisions
          (patient_id,category,record_id,record_version,image_id,seq)''',
    ):
        db.execute(statement)


class VisualMemoryMixin:
    def visual_context(self, record):
        """Keep older images after clinical edits, but never index stale interpretations."""
        rows = self.db.execute('''SELECT * FROM memory_visual_revisions WHERE patient_id=? AND category=?
            AND record_id=? AND record_version<=? ORDER BY seq DESC LIMIT 301''',
            (self.patient_id, record["category"], record["record_id"], record["version"])).fetchall()
        if len(rows) > 300:
            raise MemoryError("visual_scan_limit", "Record visual history exceeds the bounded baseline index")
        images, seen = [], set()
        for row in rows:
            if row["image_id"] in seen:
                continue
            seen.add(row["image_id"])
            value = json.loads(row["payload"])
            images.append({**value, "annotation_version": row["record_version"],
                           "stale": row["record_version"] != record["version"]})
        return images

    def link_visual(self, reference, image, regions, findings, source_run, event_id):
        """Called only after the owning patient's image admission and grounding checks."""
        from team_schemas import REGION, FINDING, array, check_output
        validate(reference, REFERENCE)
        validate(image, IMAGE)
        validate(regions, array(REGION, 6))
        validate(findings, array(FINDING, 30))
        validate(source_run, text()); validate(event_id, text())
        if reference["category"] != "lesion":
            raise MemoryError("invalid_binding", "Visual phenotype belongs to a lesion observation")
        check_output("visual", {"regions": regions[:2], "findings": findings, "quality_issues": [], "mask_sensitivity": []})
        if any(r["image_id"] != image["image_id"] for r in regions) or any(f["image_id"] != image["image_id"] for f in findings):
            raise MemoryError("invalid_binding", "Visual evidence must refer to its linked original image")
        payload = json.dumps({"reference": reference, "image": image, "regions": regions,
                              "findings": findings, "source_run": source_run, "source": "model", "review_status": "unreviewed"}, ensure_ascii=False, sort_keys=True)
        with self.transaction():
            current = self.get({"category": "lesion", "record_id": reference["record_id"], "include_versions": False})["record"]
            if current["version"] != reference["version"]:
                raise MemoryError("version_conflict", "Link images to the exact current memory version")
            old = self.db.execute("SELECT payload FROM memory_visual_revisions WHERE patient_id=? AND event_id=?", (self.patient_id, event_id)).fetchone()
            if old:
                if old["payload"] != payload:
                    raise MemoryError("event_conflict", "Visual event id cannot change")
                return {"idempotent": True, "reference": reference}
            self.db.execute('''INSERT INTO memory_visual_revisions
                (patient_id,category,record_id,record_version,image_id,event_id,payload,created_at)
                VALUES(?,?,?,?,?,?,?,?)''', (self.patient_id, "lesion", reference["record_id"], reference["version"],
                image["image_id"], event_id, payload, datetime.now(timezone.utc).isoformat()))
        return {"idempotent": False, "reference": reference}
