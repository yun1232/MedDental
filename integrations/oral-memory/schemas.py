"""JSON schemas shared by MCP discovery and durable-record validation."""

from copy import deepcopy


def text(*, empty=False):
    """Return a bounded text field; empty values require an explicit opt-in."""
    return {"type": "string", "minLength": 0 if empty else 1, "maxLength": 12000}


def obj(properties, required=()):
    """Return a closed object so images and undeclared metadata are refused."""
    return {"type": "object", "properties": properties,
            "required": list(required), "additionalProperties": False}


def choice(*values):
    """Return a closed string enumeration."""
    return {"type": "string", "enum": list(values)}


def strings():
    """Return a bounded list of distinct nonempty strings."""
    return {"type": "array", "items": text(), "maxItems": 100, "uniqueItems": True}


PHENOTYPE = {
    "site": text(), "side": choice("unknown", "left", "right", "midline", "bilateral"),
    "tooth_positions": strings(), "color": text(), "morphology": text(),
    "surface": text(), "border": text(), "extent": text(),
}
PATHOLOGY = obj({
    "status": choice("unknown", "report_provided", "doctor_confirmed"),
    "result": text(empty=True), "source_ref": text(empty=True),
}, ("status", "result", "source_ref"))
COMMON = {
    "record_id": text(), "encounter_id": text(), "event_time": text(),
    "source_ref": text(), "source": choice("patient", "model", "report", "doctor"),
    "review_status": choice("unreviewed", "doctor_confirmed"),
}
LINKS = {"lesion_ids": strings()}
RECORDS = {
    "lesion": obj({**COMMON, "lesion_id": text(), "features": obj(PHENOTYPE, PHENOTYPE),
                   "size_description": text(), "pathology": PATHOLOGY, "visual_description": text(empty=True)},
                  (*COMMON, "lesion_id", "features", "size_description", "pathology")),
    "experience": obj({**COMMON, **LINKS,
                       "experience_type": choice("symptom", "habit", "injury", "self_care", "other_oral"),
                       "description": text()},
                      (*COMMON, "lesion_ids", "experience_type", "description")),
    "care": obj({**COMMON, **LINKS,
                 "care_type": choice("visit", "examination", "diagnosis", "surgery", "medication", "follow_up"),
                 "description": text(), "outcome": text(empty=True)},
                (*COMMON, "lesion_ids", "care_type", "description", "outcome")),
    "doctor": obj({**COMMON, **LINKS,
                   "note_type": choice("supplement", "correction", "follow_up", "final_diagnosis"),
                   "description": text(), "related_record_ids": strings(),
                   "question_status": choice("not_applicable", "open", "answered", "cancelled"),
                   "answer": text(empty=True)},
                  (*COMMON, "lesion_ids", "note_type", "description", "related_record_ids",
                   "question_status", "answer")),
}
WRITE_SCHEMAS = {
    category: obj({"record": schema,
                   "expected_version": {"type": "integer", "minimum": 0}},
                  ("record", "expected_version"))
    for category, schema in RECORDS.items()
}
PATIENT_EXPERIENCE = obj({**COMMON, **LINKS, "event_kind": choice("oral", "care"),
    "description": text(), "experience_type": RECORDS["experience"]["properties"]["experience_type"],
    "care_type": RECORDS["care"]["properties"]["care_type"], "outcome": text(empty=True)},
    (*COMMON, "lesion_ids", "event_kind", "description"))
PATIENT_EXPERIENCE_WRITE = obj({"record": PATIENT_EXPERIENCE, "expected_version": {"type": "integer", "minimum": 0}}, ("record", "expected_version"))
QUERY_FEATURES = obj({**PHENOTYPE, "text": text()})
QUERY_FEATURES["minProperties"] = 1
RETRIEVE = obj({
    "lesion_query": QUERY_FEATURES, "care_query": text(empty=True),
    "experience_query": text(empty=True), "doctor_query": text(empty=True),
    "encounter_id": text(), "exclude_record_ids": strings(),
    "top_k": {"type": "integer", "minimum": 1, "maximum": 50},
    "purpose": choice("legacy", "visual", "diagnosis"), "patient_experience_query": text(empty=True),
}, ("lesion_query", "care_query", "experience_query", "doctor_query", "top_k"))
SEARCH = obj({
    "category": choice(*RECORDS, "patient_experience"), "query": text(empty=True),
    "encounter_id": text(), "lesion_id": text(),
    "note_type": choice("supplement", "correction", "follow_up", "final_diagnosis"),
    "question_status": choice("not_applicable", "open", "answered", "cancelled"),
    "top_k": {"type": "integer", "minimum": 1, "maximum": 50},
}, ("category", "query", "top_k"))
GET = obj({"category": choice(*RECORDS), "record_id": text(),
           "include_versions": {"type": "boolean"}}, ("category", "record_id", "include_versions"))

REFERENCE = obj({"category": choice(*RECORDS), "record_id": text(),
                 "version": {"type": "integer", "minimum": 1}}, ("category", "record_id", "version"))
PATIENT_EXPERIENCE_GET = obj({"reference": REFERENCE, "include_versions": {"type": "boolean"}}, ("reference", "include_versions"))
FEEDBACK = obj({"event_id": text(), "retrieval_id": text(), "reference": REFERENCE,
                "kind": choice("used", "helpful", "not_helpful", "unused_opportunity"),
                "reason": text()}, ("event_id", "retrieval_id", "reference", "kind", "reason"))
FREQUENCY = obj({"reference": REFERENCE, "frequency": choice("normal", "low_frequency"),
                 "protected": {"type": "boolean"}, "reason": text()},
                ("reference", "frequency", "protected", "reason"))
OBSERVATION = obj({"field": text(), "status": choice("known", "uncertain", "denied", "deferred", "conflict"),
                   "value": text(empty=True), "source_ref": text(),
                   "resolves_conflict": {"type": "boolean"}, "event_time": text(),
                   "memory_refs": {"type": "array", "items": REFERENCE, "maxItems": 20}},
                  ("field", "status", "value", "source_ref", "memory_refs"))
CASE = obj({"encounter_id": text()}, ("encounter_id",))
PATIENT_TURN = obj({
    "encounter_id": text(), "event_id": text(), "expected_revision": {"type": "integer", "minimum": 0},
    "message": text(), "intent": choice("provide_information", "ask_question", "pause"),
    "observations": {"type": "array", "items": OBSERVATION, "maxItems": 30},
    "writes": {"type": "array", "items": obj({"category": choice("lesion", "patient_experience", "experience", "care"),
        "record": {"type": "object", "properties": {}, "additionalProperties": True},
        "expected_version": {"type": "integer", "minimum": 0}},
        ("category", "record", "expected_version")), "maxItems": 30},
    "doctor_answers": {"type": "array", "items": obj({"record_id": text(),
        "version": {"type": "integer", "minimum": 1}, "answer": text(),
        "sufficient": {"type": "boolean"}, "source_ref": text()},
        ("record_id", "version", "answer", "sufficient", "source_ref")), "maxItems": 10},
}, ("encounter_id", "event_id", "expected_revision", "message", "intent", "observations", "writes", "doctor_answers"))
PATIENT_DECIDE = obj({"encounter_id": text(), "event_id": text(), "expected_revision": {"type": "integer", "minimum": 0},
    "retrieval_id": text(), "question": text(), "field": text(), "case_refs": {"type": "array", "items": REFERENCE, "maxItems": 12}},
    ("encounter_id", "event_id", "expected_revision", "retrieval_id", "question", "field", "case_refs"))
DOCTOR_SUBMIT = obj({
    "encounter_id": text(), "event_id": text(), "expected_revision": {"type": "integer", "minimum": 0},
    "confirmed": {"type": "boolean"}, "note": RECORDS["doctor"],
    "expected_note_version": {"type": "integer", "minimum": 0}, "target_field": text(empty=True),
    "observations": {"type": "array", "items": OBSERVATION, "maxItems": 30},
    "updates": {"type": "array", "items": obj({"category": choice("lesion", "patient_experience", "experience", "care"),
        "record": {"type": "object", "properties": {}, "additionalProperties": True},
        "expected_version": {"type": "integer", "minimum": 0}},
        ("category", "record", "expected_version")), "maxItems": 30},
}, ("encounter_id", "event_id", "expected_revision", "confirmed", "note", "expected_note_version", "target_field", "observations", "updates"))


class MemoryError(ValueError):
    """An actionable domain error that may be returned to an MCP caller."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def validate(value, schema, path="input"):
    """Validate the schema subset declared in this module at JSON boundaries."""
    kind = schema.get("type")
    valid = {"object": isinstance(value, dict), "array": isinstance(value, list),
             "string": isinstance(value, str), "integer": type(value) is int,
             "boolean": type(value) is bool}.get(kind, False)
    if not valid:
        raise MemoryError("invalid_input", f"{path}: expected {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise MemoryError("invalid_input", f"{path}: value is outside the allowed enumeration")
    if kind == "object":
        missing = set(schema.get("required", [])) - value.keys()
        extra = set() if schema.get("additionalProperties") is True else value.keys() - schema["properties"].keys()
        if missing or extra or len(value) < schema.get("minProperties", 0):
            raise MemoryError("invalid_input", f"{path}: missing={sorted(missing)}, extra={sorted(extra)}")
        for key, item in value.items():
            if key in schema["properties"]:
                validate(item, schema["properties"][key], f"{path}.{key}")
    elif kind in ("array", "string"):
        minimum = schema.get("minLength", 0) if kind == "string" else 0
        maximum = schema.get("maxLength", 12000) if kind == "string" else schema.get("maxItems", 100)
        if not minimum <= len(value) <= maximum:
            raise MemoryError("invalid_input", f"{path}: length is outside allowed bounds")
        if kind == "string" and minimum and not value.strip():
            raise MemoryError("invalid_input", f"{path}: blank text is not allowed")
        if kind == "array":
            for index, item in enumerate(value):
                validate(item, schema["items"], f"{path}[{index}]")
            if schema.get("uniqueItems") and len(set(value)) != len(value):
                raise MemoryError("invalid_input", f"{path}: duplicate items")
    elif kind == "integer" and not schema.get("minimum", value) <= value <= schema.get("maximum", value):
        raise MemoryError("invalid_input", f"{path}: integer is outside allowed bounds")


def tools(role):
    """Build MCP tool declarations for a host-bound actor role."""
    result = []
    for category, schema in WRITE_SCHEMAS.items():
        if category == "doctor" and role != "doctor":
            continue
        result.append({"name": f"put_{category}", "description": (
            f"Persist one oral-leukoplakia {category} record for the host-bound patient. "
            "Create with expected_version=0; update with the exact current version. "
            "Keep source_ref and event_time. Unknown image features must stay unknown. "
            "Never infer histopathology from an image. Only a doctor host may confirm records."
        ), "inputSchema": deepcopy(schema)})
    result.extend([
        {"name": "put_patient_experience", "description": "Persist one unified patient experience: oral symptoms/habits or care/examination/treatment/follow-up. event_kind selects the subtype; source, versions and original feedback references are preserved.", "inputSchema": PATIENT_EXPERIENCE_WRITE},
        {"name": "get_patient_experience", "description": "Read one unified patient experience using its stable storage_reference; oral/care records remain one business category.", "inputSchema": PATIENT_EXPERIENCE_GET},
        {"name": "case_state", "description": "Read persistent inquiry fields, pending questions and case revision.", "inputSchema": CASE},
        {"name": "patient_decide", "description": "Patient communication agent persists one question grounded in retrieved cases, with receipt references and exact case revision. Use after patient_turn returns decide_from_cases.", "inputSchema": PATIENT_DECIDE},
        {"name": "patient_turn", "description": "Commit extracted patient information and memory writes atomically; return one next question with memory context. Use event_id for retry and the current case revision.", "inputSchema": PATIENT_TURN},
        {"name": "memory_feedback", "description": "Record actual use of a delivered memory reference. Patient callers may report used only; doctor feedback controls utility. Scores are never supplied by the model.", "inputSchema": FEEDBACK},
        {"name": "retrieve", "description": (
            "Select purpose=visual for patient-experience-only retrieval before visual analysis; purpose=diagnosis for lesion descriptors plus patient experience after visual analysis. The default legacy mode retains historical clients: stage 1 recalls lesion descriptors; stage 2 "
            "scores ONLY associated care records. Strict order is phenotype score first, care "
            "score second (priority levels, not numeric weights). Experience and doctor notes "
            "are searched separately and never change that order. Empty phenotype needs clarification."
        ), "inputSchema": RETRIEVE},
        {"name": "search", "description": (
            "Search exactly one memory category. Empty query lists recent writes explicitly; "
            "use this for known history, pending doctor questions, or absent phenotype. "
            "Category searches do not silently replace hierarchical retrieval."
        ), "inputSchema": SEARCH},
        {"name": "get", "description": "Read a record and optional immutable revisions for the bound patient.",
         "inputSchema": GET},
        {"name": "instructions", "description": "Read oral-memory extraction, separate storage and retrieval instructions.",
         "inputSchema": obj({})},
    ])
    if role == "doctor":
        result.extend([
            {"name": "doctor_submit", "description": "Submit an explicitly confirmed doctor note, optional corrections and inquiry updates atomically.", "inputSchema": DOCTOR_SUBMIT},
            {"name": "set_frequency", "description": "Set normal or low_frequency and protection; no memory deletion exists.", "inputSchema": FREQUENCY},
        ])
    return result
